"""The real ``biff session`` verbs run against a real daemon process."""

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


def _biff(repo: Path, home: Path, *args: str) -> tuple[int, str, str]:
    """Run ``biff --user kai *args`` in *repo*; return its status, stdout, stderr."""
    env = {**os.environ, "HOME": str(home), "PUNT_TELEMETRY": "off"}
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "biff", "--user", "kai", *args],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate(timeout=_TIMEOUT)
    return proc.returncode, out, err


def test_a_session_is_made_listed_and_ended_through_the_daemon(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    made = _biff(repo, tmp_path, "session", "new", "--tty", "main")
    listed = _biff(repo, tmp_path, "session", "ls")
    ended = _biff(repo, tmp_path, "session", "end", "main")
    after = _biff(repo, tmp_path, "session", "ls", "--all")
    assert made == (0, "main\n", "")
    assert "main" in listed[1]
    assert ended[:2] == (0, "ended main\n")
    assert "ended" in after[1]


def test_an_unknown_session_name_exits_one_on_stderr(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    result = _biff(repo, tmp_path, "session", "use", "nope")
    assert result == (1, "", "Error: unknown session: nope\n")
