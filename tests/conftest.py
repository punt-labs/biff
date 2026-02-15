"""Shared test fixtures for biff."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


def _find_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def nats_server() -> Iterator[str]:
    """Start a nats-server with JetStream for the test session.

    Returns the server URL (e.g. ``nats://127.0.0.1:14222``).
    Shared across all test directories that need a NATS server.
    """
    exe = shutil.which("nats-server")
    if exe is None:
        pytest.skip("nats-server not found on PATH")

    port = _find_free_port()
    url = f"nats://127.0.0.1:{port}"

    proc = subprocess.Popen(  # noqa: S603
        [exe, "-js", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("nats-server did not start within 5 seconds")

    yield url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
