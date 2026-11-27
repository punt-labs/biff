"""The real ``biff watch`` process prints arrivals from a real daemon."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient

if TYPE_CHECKING:
    from pathlib import Path

    from biff.run_dir import RunDir

pytestmark = pytest.mark.subprocess

_EXIT_TIMEOUT = 15


def _next_line(watch: subprocess.Popen[str]) -> dict[str, object]:
    assert watch.stdout is not None
    return cast("dict[str, object]", json.loads(watch.stdout.readline()))


async def test_biff_watch_prints_each_arrival_and_ends_on_sigterm(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    env = {**os.environ, "HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    watch = subprocess.Popen(
        [sys.executable, "-m", "biff", "--user", "kai", "watch"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    eric = await DaemonClient.connect(
        run_dir=run_dir,
        identity=ClientIdentity(
            client="cli", session_id="s-eric", cwd=str(repo), user="eric"
        ),
    )
    try:
        first = _next_line(watch)
        await eric.request("write", to="kai", message="secret body")
        second = _next_line(watch)
        watch.send_signal(signal.SIGTERM)
        out, err = watch.communicate(timeout=_EXIT_TIMEOUT)
    finally:
        watch.kill()
        await eric.close()
    assert (first["kind"], second["kind"]) == ("unread", "mail")
    assert (watch.returncode, out, err) == (0, "", "")


async def test_biff_watch_ends_when_its_reader_leaves_even_with_no_event(
    run_dir: RunDir, repo: Path, tmp_path: Path
) -> None:
    env = {**os.environ, "HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    watch = subprocess.Popen(
        [sys.executable, "-m", "biff", "--user", "kai", "watch"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _next_line(watch)  # what `biff watch | head -n 1` reads
        assert watch.stdout is not None
        watch.stdout.close()
        status = watch.wait(timeout=_EXIT_TIMEOUT)
    finally:
        watch.kill()
        watch.wait()
        assert watch.stderr is not None
        watch.stderr.close()
    assert status == 0
