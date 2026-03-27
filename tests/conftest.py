"""Shared test fixtures for biff."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _silence_vox(request: pytest.FixtureRequest) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Prevent vox from speaking during tests.

    Patches ``vox_binary`` to return ``None`` so
    ``speak_fire_and_forget`` is a no-op — no audio output,
    no subprocess spawned, no system state changed.

    Tests marked ``@pytest.mark.vox`` opt out and manage
    vox mocking themselves.
    """
    node = request.node  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    if node.get_closest_marker("vox"):  # pyright: ignore[reportUnknownMemberType]
        yield
        return
    with patch("biff.integration.vox.vox_binary", return_value=None):
        yield


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
