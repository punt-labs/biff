"""The real ``biff daemon serve`` process: start, answer /health, stop on SIGTERM."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
import urllib.request
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.subprocess

_STARTUP_TIMEOUT = 15.0


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


def test_serve_answers_health_and_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    run = tmp_path / ".punt-labs" / "biff" / "run"
    env = {**os.environ, "HOME": str(tmp_path), "PUNT_TELEMETRY": "off"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(run / "serve.port")
        port = int((run / "serve.port").read_text())
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=5) as response:
            assert response.status == 200
        assert stat.S_IMODE((run / "serve.token").stat().st_mode) == 0o600
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert not (run / "serve.port").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
