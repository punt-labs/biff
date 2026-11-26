"""A connection is trusted only after a valid hello, and is bound to its session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from biff.client.connection import DaemonError
from biff.frames import (
    PROTOCOL_VERSION,
    ErrorCode,
    Frames,
    Hello,
    Reply,
    Request,
)

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness


def _hello(harness: Harness, **overrides: object) -> Hello:
    fields: dict[str, object] = {
        "token": harness.run_dir.read_token(),
        "client": "cli",
        "session_id": "s-kai",
        "main_agent_id": "m1",
        "cwd": "/work/repo",
        "user": "kai",
        **overrides,
    }
    return Hello.model_validate(fields)


async def _closes_with(harness: Harness, *frames: str) -> int:
    """Send *frames*; return the close code the daemon answers with."""
    url = f"ws://127.0.0.1:{harness.daemon.port}/"
    async with connect(url) as ws:
        for frame in frames:
            await ws.send(frame)
        with pytest.raises(ConnectionClosed):
            while True:  # a hello is acknowledged before the close
                await asyncio.wait_for(ws.recv(), timeout=5)
        assert ws.close_code is not None
        return ws.close_code


async def test_wrong_token_closes_the_connection(harness: Harness) -> None:
    hello = _hello(harness, token="not-the-token")
    assert await _closes_with(harness, Frames.encode(hello)) == 1008


async def test_wrong_token_changes_no_state(harness: Harness) -> None:
    hello = _hello(harness, token="not-the-token")
    await _closes_with(harness, Frames.encode(hello))
    status = harness.daemon.status()
    assert (status.relay_connections, status.sessions) == (0, ())
    assert harness.factory.built == []


async def test_request_before_hello_closes_the_connection(harness: Harness) -> None:
    request = Request(id=1, method="plan", params={"message": "sneaky"})
    assert await _closes_with(harness, Frames.encode(request)) == 1008


async def test_request_before_hello_changes_no_state(harness: Harness) -> None:
    request = Request(id=1, method="plan", params={"message": "sneaky"})
    await _closes_with(harness, Frames.encode(request))
    status = harness.daemon.status()
    assert (status.relay_connections, status.sessions) == (0, ())


async def test_garbage_before_hello_closes_the_connection(harness: Harness) -> None:
    assert await _closes_with(harness, "{not json") == 1008


async def test_a_second_hello_closes_the_connection(harness: Harness) -> None:
    hello = Frames.encode(_hello(harness))
    assert await _closes_with(harness, hello, hello) == 1008


async def test_unsupported_version_names_both_versions_and_the_fix(
    harness: Harness,
) -> None:
    hello = _hello(harness, version=PROTOCOL_VERSION + 1)
    url = f"ws://127.0.0.1:{harness.daemon.port}/"
    async with connect(url) as ws:
        await ws.send(Frames.encode(hello))
        reply = Frames.decode(str(await ws.recv()))
    assert isinstance(reply, Reply)
    assert reply.error is not None
    assert reply.error.code is ErrorCode.UNSUPPORTED_VERSION
    message = reply.error.message
    assert str(PROTOCOL_VERSION) in message
    assert str(PROTOCOL_VERSION + 1) in message
    assert "biff daemon restart" in message


async def test_unsupported_version_registers_no_session(harness: Harness) -> None:
    hello = _hello(harness, version=PROTOCOL_VERSION + 1)
    url = f"ws://127.0.0.1:{harness.daemon.port}/"
    async with connect(url) as ws:
        await ws.send(Frames.encode(hello))
        await ws.recv()
    assert harness.daemon.status().sessions == ()


async def test_a_browser_origin_is_refused(harness: Harness) -> None:
    url = f"ws://127.0.0.1:{harness.daemon.port}/"
    with pytest.raises(Exception, match="403"):
        async with connect(url, origin=Origin("http://evil.example")):
            pass


async def test_hello_for_an_unconfigured_directory_is_refused(
    harness: Harness,
) -> None:
    with pytest.raises(DaemonError) as caught:
        await harness.connect("s-kai", "kai", cwd="/work/missing")
    assert caught.value.code is ErrorCode.BAD_HELLO
