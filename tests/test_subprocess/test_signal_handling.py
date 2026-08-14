"""Real-subprocess regression tests for SIGTERM/SIGINT/SIGHUP handling.

The signal handler in ``_active_lifespan`` writes a cleanup sentinel and
must then actually terminate the process.  ``signal.signal()`` replaces the
platform's default terminate disposition, so a handler that merely returns
leaves the process running forever -- ``kill <pid>`` would do nothing but
touch a file.

Only a real subprocess receiving a real OS signal can catch that.  Calling
``_signal_handler`` directly, in-process, proves nothing: the failure mode
under test is a handler that *returns* and leaves the process alive, and an
in-process call has no process lifetime to observe.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_MARKER = "Starting MCP server"
_STARTUP_TIMEOUT = 15.0
_GRACE_PERIOD = 5.0


def _sentinel_dir(home: Path) -> Path:
    """Sentinel directory the running server writes to under isolated HOME."""
    return home / ".punt-labs" / "biff" / "sentinels" / "punt-labs__biff"


def _wait_for_startup(proc: subprocess.Popen[str]) -> None:
    """Block until the server has passed through active-lifespan startup.

    ``_active_lifespan`` -- which registers the signal handlers under test
    -- runs to completion before FastMCP logs this line (the lifespan context
    manager is entered before the stdio transport starts), so waiting for it
    is both correct and far faster than a fixed sleep.  Reads stderr on a
    background thread so a hung startup can't block this wait forever.
    """
    assert proc.stderr is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=_pump, daemon=True).start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0))
        except queue.Empty:
            break
        if line is None:
            raise AssertionError(
                f"server exited during startup with code {proc.returncode}"
            )
        if _STARTUP_MARKER in line:
            return
    raise AssertionError(f"server did not start within {_STARTUP_TIMEOUT}s")


def _wait_for_exit(proc: subprocess.Popen[str], grace: float = _GRACE_PERIOD) -> int:
    """Poll for process exit, raising if it survives the grace period."""
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            return code
        time.sleep(0.05)
    raise AssertionError(
        f"process {proc.pid} survived {grace}s grace period after signal"
    )


@pytest.fixture
def active_server(tmp_path: Path) -> Generator[tuple[subprocess.Popen[str], Path]]:
    """Spawn a real, non-dormant ``biff mcp`` subprocess.

    Isolated HOME redirects ``biff_data_dir()`` (``~/.punt-labs/biff``) so
    the sentinel/active-session files this test asserts on never touch the
    operator's real data.  cwd=_REPO_ROOT so ``is_enabled()`` sees this
    repo's committed ``.punt-labs/biff/enabled`` marker -- signal handlers
    are registered only in the *active* (non-dormant) lifespan, so a
    dormant server would prove nothing about the defect.
    """
    home = tmp_path / "home"
    home.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    # tmp_path's leaf directory name is unique per test invocation
    # (pytest appends a numeric suffix per parametrize case), so it
    # doubles as a collision-free username for this run.
    user = f"sigtest-{tmp_path.name}".lower()
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "biff",
            "mcp",
            "--user",
            user,
            "--data-dir",
            str(data_dir),
            "--relay-url",
            "",
        ],
        cwd=_REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_startup(proc)
    try:
        yield proc, home
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda s: s.name
)
class TestSignalTerminatesProcess:
    """SIGTERM, SIGINT, and SIGHUP each terminate the server after cleanup."""

    def test_process_exits_and_sentinel_precedes_exit(
        self, active_server: tuple[subprocess.Popen[str], Path], sig: signal.Signals
    ) -> None:
        """Sending *sig* to a real subprocess ends it and leaves a sentinel.

        The sentinel assertion proves cleanup ran -- rather than the process
        vanishing via an abrupt exit that skips it -- and the exit-code
        assertion proves the signal terminated it, not the OS default action
        firing before our handler ran (which would also leave no sentinel).
        """
        proc, home = active_server
        os.kill(proc.pid, sig)
        exit_code = _wait_for_exit(proc)

        # Restoring the default disposition and re-delivering the signal to
        # ourselves (see the handler) makes the OS terminate the process, so
        # Python reports the standard "killed by signal N" code: -N.
        assert exit_code == -sig.value, (
            f"expected termination by {sig.name} (exit code {-sig.value}), "
            f"got {exit_code}"
        )

        sentinels = list(_sentinel_dir(home).glob("*"))
        assert sentinels, "signal handler did not write its cleanup sentinel"
