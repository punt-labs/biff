"""The real ``biff`` REPL and ``biff talk`` run against a real daemon."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from biff.run_dir import RunDir

pytestmark = pytest.mark.subprocess

_TIMEOUT = 30


def _biff(
    repo: Path, home: Path, *args: str, stdin: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "PUNT_TELEMETRY": "off"}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "biff", "--user", "kai", *args],
        input=stdin,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
        check=False,
    )


def test_the_repl_runs_commands_in_the_real_daemon_until_exit(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    done = _biff(repo, tmp_path, stdin="plan ship biffd\nwho\nexit\n")
    assert (done.returncode, done.stderr) == (0, "")
    assert "biff " in done.stdout
    assert "ship biffd" in done.stdout


def test_the_repl_ends_at_end_of_input(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    done = _biff(repo, tmp_path, stdin="status\n")
    assert (done.returncode, done.stderr) == (0, "")
    assert "unread:" in done.stdout


def test_biff_talk_says_when_the_relay_carries_no_talk_and_exits_1(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    done = _biff(repo, tmp_path, "talk", "eric:tty1", stdin="")
    assert done.returncode == 1
    assert "Talk requires a NATS relay connection." in done.stdout
