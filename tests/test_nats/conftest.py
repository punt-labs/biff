"""Fixtures for NatsRelay tests.

Manages a real ``nats-server -js`` subprocess per test session and
provides a :class:`~biff.nats_relay.NatsRelay` connected to it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


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


@pytest.fixture
async def relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """Provide a connected NatsRelay and clean up after each test.

    Purges all streams and KV buckets between tests for isolation.
    """
    r = NatsRelay(url=nats_server)

    yield r

    # Clean up: delete stream (removes durable consumers) and KV bucket.
    # close() resets all cached state (_nc, _js, _kv) to None.
    if r._nc is not None and r._js is not None:
        with suppress(Exception):
            await r._js.delete_stream("BIFF_INBOX")
        with suppress(Exception):
            await r._js.delete_key_value("biff-sessions")  # pyright: ignore[reportUnknownMemberType]

    await r.close()


@pytest.fixture
async def second_relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """A second relay instance for cross-user tests."""
    r = NatsRelay(url=nats_server)
    yield r
    await r.close()
