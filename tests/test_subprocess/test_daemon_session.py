"""The real daemon process serves a real client against a real repository."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.subprocess

_STARTUP_TIMEOUT = 15.0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository the daemon's own configuration loader can resolve."""
    path = tmp_path / "work" / "demo"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path


@pytest.fixture
def run_dir(tmp_path: Path) -> Iterator[RunDir]:
    env = {**os.environ, "HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run = RunDir(tmp_path / ".punt-labs" / "biff" / "run")
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not run.port_path.exists():
        assert time.monotonic() < deadline, "daemon never wrote its port file"
        time.sleep(0.05)
    yield run
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)


async def test_two_sessions_share_a_repository_through_the_real_daemon(
    run_dir: RunDir, repo: Path
) -> None:
    def identity(session_id: str, user: str) -> ClientIdentity:
        return ClientIdentity(
            client="cli", session_id=session_id, cwd=str(repo), user=user
        )

    kai = await DaemonClient.connect(run_dir=run_dir, identity=identity("s-kai", "kai"))
    eric = await DaemonClient.connect(
        run_dir=run_dir, identity=identity("s-eric", "eric")
    )
    try:
        await kai.request("plan", message="end to end")
        who = cast("dict[str, list[dict[str, str]]]", await eric.request("who"))
        assert {row["user"]: row["plan"] for row in who["data"]}["kai"] == "end to end"
    finally:
        await kai.close()
        await eric.close()
