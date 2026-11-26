"""A connection is proven both ways, then trusted only after a valid hello."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from biff.client.connection import (
    ClientIdentity,
    DaemonClient,
    DaemonError,
    UnprovenDaemonError,
)
from biff.frame_base import PROTOCOL_VERSION
from biff.frame_client import Hello, Request
from biff.frame_proof import Challenge, Proof
from biff.frame_reply import ErrorCode, Reply
from biff.frames import Frames
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_TOKEN_MARKER = "impostor-must-never-see-this-token"


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


async def _proven(harness: Harness) -> ClientConnection:
    """Open a connection and complete the daemon's proof of the token."""
    ws = await connect(f"ws://127.0.0.1:{harness.daemon.port}/")
    challenge = Challenge(nonce="ab" * 16)
    await ws.send(Frames.encode(challenge))
    proof = Frames.decode(str(await ws.recv()))
    assert isinstance(proof, Proof)
    assert proof.answers(challenge, harness.run_dir.read_token())
    return ws


async def _closes_with(harness: Harness, *frames: str, prove: bool = True) -> int:
    """Send *frames*; return the close code the daemon answers with."""
    if prove:
        ws = await _proven(harness)
    else:
        ws = await connect(f"ws://127.0.0.1:{harness.daemon.port}/")
    async with ws:
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


async def test_hello_without_a_challenge_closes_the_connection(
    harness: Harness,
) -> None:
    hello = Frames.encode(_hello(harness))
    assert await _closes_with(harness, hello, prove=False) == 1008


async def test_hello_without_a_challenge_changes_no_state(harness: Harness) -> None:
    await _closes_with(harness, Frames.encode(_hello(harness)), prove=False)
    assert harness.daemon.status().sessions == ()


async def test_a_second_challenge_closes_the_connection(harness: Harness) -> None:
    again = Frames.encode(Challenge(nonce="cd" * 16))
    assert await _closes_with(harness, again) == 1008


async def test_a_short_nonce_is_refused(harness: Harness) -> None:
    raw = json.dumps({"type": "challenge", "version": 1, "nonce": "ab"})
    assert await _closes_with(harness, raw, prove=False) == 1008


async def test_unsupported_version_names_both_versions_and_the_fix(
    harness: Harness,
) -> None:
    hello = _hello(harness, version=PROTOCOL_VERSION + 1)
    async with await _proven(harness) as ws:
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
    async with await _proven(harness) as ws:
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


class Impostor:
    """A listener on the daemon's port that does not know the token."""

    def __init__(self, answer: str | None) -> None:
        self.received: list[str] = []
        self._answer = answer

    async def handle(self, ws: ServerConnection) -> None:
        async for raw in ws:
            self.received.append(str(raw))
            if self._answer is not None:
                await ws.send(self._answer)
            else:
                await asyncio.sleep(30)


def _run_dir_with_token(tmp_path: Path) -> RunDir:
    run_dir = RunDir(tmp_path / "run")
    run_dir.ensure()
    run_dir.token_path.write_text(_TOKEN_MARKER)
    run_dir.token_path.chmod(0o600)
    return run_dir


def _identity() -> ClientIdentity:
    return ClientIdentity(client="cli", session_id="s-kai", cwd="/work/repo")


async def test_an_impostor_with_a_wrong_proof_never_receives_the_token(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir_with_token(tmp_path)
    forged = Frames.encode(Proof(mac="00" * 32))
    impostor = Impostor(forged)
    async with serve(impostor.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(UnprovenDaemonError, match="did not prove"):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert impostor.received
    assert _TOKEN_MARKER not in "".join(impostor.received)
    assert all('"hello"' not in frame for frame in impostor.received)


async def test_an_impostor_that_stays_silent_never_receives_the_token(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir_with_token(tmp_path)
    impostor = Impostor(None)
    async with serve(impostor.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(UnprovenDaemonError, match="did not prove"):
            await DaemonClient.connect(
                run_dir=run_dir, identity=_identity(), port=port, proof_timeout=0.2
            )
    assert _TOKEN_MARKER not in "".join(impostor.received)


async def test_an_impostor_that_replays_the_challenge_never_gets_the_token(
    tmp_path: Path,
) -> None:
    """Echoing the nonce back is not a proof."""
    run_dir = _run_dir_with_token(tmp_path)
    received: list[str] = []

    async def echo(ws: ServerConnection) -> None:
        async for raw in ws:
            received.append(str(raw))
            nonce = Frames.decode(str(raw))
            assert isinstance(nonce, Challenge)
            await ws.send(Frames.encode(Proof(mac=nonce.nonce)))

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(UnprovenDaemonError):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert _TOKEN_MARKER not in "".join(received)
