"""biffd writes to biff.log when it starts and when it listens."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from biff.daemon.environment import DaemonEnvironment

_PATIENCE_SECONDS: Final = 60.0


class Run(NamedTuple):
    """A foreground biffd that has listened and been stopped."""

    pid: int
    lines: list[str]


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / ".punt-labs" / "biff" / "logs" / "biff.log"


def _foreground_biffd(home: Path) -> subprocess.Popen[bytes]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in DaemonEnvironment.scrub(dict(os.environ))
    }
    env["HOME"] = str(home)
    env.pop("XDG_CONFIG_HOME", None)
    return subprocess.Popen(
        [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _log_until_listening(log: Path) -> list[str]:
    deadline = time.monotonic() + _PATIENCE_SECONDS
    while time.monotonic() < deadline:
        lines = log.read_text().splitlines() if log.exists() else []
        if any("biffd listening on" in line for line in lines):
            return lines
        time.sleep(0.1)
    pytest.fail(f"biffd never logged that it listens; log: {log.read_text()!r}")


@pytest.fixture
def run(tmp_path: Path, log: Path) -> Run:
    proc = _foreground_biffd(tmp_path)
    try:
        seen = _log_until_listening(log)
    finally:
        # No test leaves a biffd running, even one that ignores SIGTERM.
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return Run(proc.pid, seen)


def test_a_foreground_start_logs_that_it_starts(run: Run) -> None:
    starting = [line for line in run.lines if "biffd starting:" in line]
    assert len(starting) == 1
    assert f"pid={run.pid}" in starting[0]


def test_the_startup_line_names_version_port_run_dir_and_interpreter(run: Run) -> None:
    starting = next(line for line in run.lines if "biffd starting:" in line)
    # The log writes the home directory as ``~``.
    expected = [
        "version=",
        "port=0",
        "run_dir=~/.punt-labs/biff/run",
        " python=",
    ]
    assert [part for part in expected if part not in starting] == []
    assert starting.endswith(sys.executable.removeprefix(str(Path.home())))


def test_a_foreground_start_logs_that_it_listens(run: Run) -> None:
    assert any("biffd listening on 127.0.0.1:" in line for line in run.lines)


def test_the_startup_line_comes_before_the_listening_line(run: Run) -> None:
    starting = next(i for i, x in enumerate(run.lines) if "biffd starting:" in x)
    listening = next(i for i, x in enumerate(run.lines) if "biffd listening" in x)
    assert starting < listening


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_is_logged_with_its_name_and_pid(
    tmp_path: Path, log: Path, sig: signal.Signals
) -> None:
    proc = _foreground_biffd(tmp_path)
    try:
        _log_until_listening(log)
        proc.send_signal(sig)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    stopping = [x for x in log.read_text().splitlines() if "biffd stopping:" in x]
    assert [x.split("biffd stopping: ")[1] for x in stopping] == [
        f"signal={sig.name} pid={proc.pid}"
    ]
