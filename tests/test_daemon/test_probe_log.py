"""A port probe is not an error: biffd logs a real client's bad handshake once."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Final, NamedTuple

import pytest

from biff.daemon.environment import DaemonEnvironment
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_PATIENCE_SECONDS: Final = 60.0
_HANDSHAKE: Final = "opening handshake failed"


class Biffd(NamedTuple):
    """A running foreground biffd: where it listens and where it writes."""

    port: int
    log: Path
    stderr: Path

    def log_text(self) -> str:
        """Return biff.log as written so far."""
        return self.log.read_text() if self.log.exists() else ""

    def wait_for_log(self, needle: str) -> None:
        """Block until *needle* is in biff.log; fail the test after a minute."""
        deadline = time.monotonic() + _PATIENCE_SECONDS
        while time.monotonic() < deadline:
            if needle in self.log_text():
                return
            time.sleep(0.05)
        pytest.fail(f"{needle!r} never reached biff.log: {self.log_text()!r}")


@pytest.fixture
def biffd(tmp_path: Path) -> Iterator[Biffd]:
    """A real biffd whose stderr is a file, as under a LaunchAgent."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in DaemonEnvironment.scrub(dict(os.environ))
    }
    env["HOME"] = str(tmp_path)
    env.pop("XDG_CONFIG_HOME", None)
    stderr = tmp_path / "stderr.txt"
    log = tmp_path / ".punt-labs" / "biff" / "logs" / "biff.log"
    with stderr.open("wb") as err:
        proc = subprocess.Popen(
            [sys.executable, "-m", "biff", "daemon", "serve", "--port", "0"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
        try:
            deadline = time.monotonic() + _PATIENCE_SECONDS
            port_path = RunDir(tmp_path / ".punt-labs" / "biff" / "run").port_path
            while not port_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            started = Biffd(RunDir(port_path.parent).read_port(), log, stderr)
            started.wait_for_log("biffd listening")
            yield started
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_a_bare_connect_and_close_leaves_no_error_and_no_traceback(
    biffd: Biffd,
) -> None:
    socket.create_connection(("127.0.0.1", biffd.port), timeout=5).close()
    # A second, unrelated line tells us the probe's record had its chance to land.
    socket.create_connection(("127.0.0.1", biffd.port), timeout=5).close()
    time.sleep(0.5)
    written = biffd.log_text() + biffd.stderr.read_text()
    assert [
        needle for needle in ("[ERROR]", "Traceback", _HANDSHAKE) if needle in written
    ] == []


def test_a_client_that_sends_garbage_is_logged_once_at_warning(
    biffd: Biffd,
) -> None:
    with socket.create_connection(("127.0.0.1", biffd.port), timeout=5) as sock:
        sock.sendall(b"NOT HTTP AT ALL\r\n\r\n")
        sock.recv(4096)
    biffd.wait_for_log(_HANDSHAKE)
    time.sleep(0.5)
    lines = [line for line in biffd.log_text().splitlines() if _HANDSHAKE in line]
    assert len(lines) == 1
    assert "[WARNING]" in lines[0]


def test_a_client_handshake_failure_is_not_written_twice(biffd: Biffd) -> None:
    with socket.create_connection(("127.0.0.1", biffd.port), timeout=5) as sock:
        sock.sendall(b"NOT HTTP AT ALL\r\n\r\n")
        sock.recv(4096)
    biffd.wait_for_log(_HANDSHAKE)
    time.sleep(0.5)
    assert _HANDSHAKE not in biffd.stderr.read_text()


def test_a_bad_handshake_carries_no_traceback(biffd: Biffd) -> None:
    with socket.create_connection(("127.0.0.1", biffd.port), timeout=5) as sock:
        sock.sendall(b"NOT HTTP AT ALL\r\n\r\n")
        sock.recv(4096)
    biffd.wait_for_log(_HANDSHAKE)
    assert "Traceback" not in biffd.log_text()
