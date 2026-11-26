"""A connection is proven both ways; the token itself never crosses the wire."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
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
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply
from biff.frames import Frames
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_TOKEN_MARKER = "impostor-must-never-see-this-token"


@dataclass
class Opened:
    """A connection that has completed the daemon's half of the handshake."""

    ws: ClientConnection
    challenge: Challenge
    proof: Proof

    def hello(self, token: str, **overrides: object) -> Hello:
        """A hello whose MAC is bound to this connection's two nonces."""
        mac = Handshake.client_mac(
            token, self.proof.daemon_nonce, self.challenge.client_nonce
        )
        fields: dict[str, object] = {
            "mac": mac,
            "client": "cli",
            "session_id": "s-kai",
            "main_agent_id": "m1",
            "cwd": "/work/repo",
            "user": "kai",
            **overrides,
        }
        return Hello.model_validate(fields)


async def _open(harness: Harness) -> Opened:
    ws = await connect(f"ws://127.0.0.1:{harness.daemon.port}/")
    challenge = Challenge.fresh()
    await ws.send(Frames.encode(challenge))
    proof = Frames.decode(str(await ws.recv()))
    assert isinstance(proof, Proof)
    assert proof.valid_for(challenge, harness.run_dir.read_token())
    return Opened(ws, challenge, proof)


async def _closes_with(ws: ClientConnection, *frames: str) -> int:
    """Send *frames* on *ws*; return the close code the daemon answers with."""
    async with ws:
        for frame in frames:
            await ws.send(frame)
        with pytest.raises(ConnectionClosed):
            while True:  # a hello is acknowledged before the close
                await asyncio.wait_for(ws.recv(), timeout=5)
        assert ws.close_code is not None
        return ws.close_code


async def _unproven(harness: Harness) -> ClientConnection:
    return await connect(f"ws://127.0.0.1:{harness.daemon.port}/")


def _no_state(harness: Harness) -> bool:
    status = harness.daemon.status()
    return (status.relay_connections, status.sessions) == (0, ())


async def test_a_hello_with_a_wrong_token_mac_closes_the_connection(
    harness: Harness,
) -> None:
    opened = await _open(harness)
    hello = opened.hello("not-the-token")
    assert await _closes_with(opened.ws, Frames.encode(hello)) == 1008


async def test_a_wrong_token_mac_changes_no_state(harness: Harness) -> None:
    opened = await _open(harness)
    await _closes_with(opened.ws, Frames.encode(opened.hello("not-the-token")))
    assert _no_state(harness)
    assert harness.factory.built == []


async def test_the_hello_frame_has_no_token_field() -> None:
    assert "token" not in Hello.model_fields


async def test_a_hello_carrying_a_token_field_is_refused(harness: Harness) -> None:
    opened = await _open(harness)
    raw = json.loads(Frames.encode(opened.hello(harness.run_dir.read_token())))
    raw["token"] = harness.run_dir.read_token()
    assert await _closes_with(opened.ws, json.dumps(raw)) == 1008


async def test_a_replayed_client_mac_is_refused_on_a_fresh_connection(
    harness: Harness,
) -> None:
    token = harness.run_dir.read_token()
    first = await _open(harness)
    captured = Frames.encode(first.hello(token))
    async with first.ws:
        await first.ws.send(captured)
        await first.ws.recv()  # the daemon accepted the genuine hello
    second = await _open(harness)
    assert await _closes_with(second.ws, captured) == 1008


async def test_a_replayed_mac_registers_no_second_session(harness: Harness) -> None:
    token = harness.run_dir.read_token()
    first = await _open(harness)
    captured = Frames.encode(first.hello(token))
    async with first.ws:
        await first.ws.send(captured)
        await first.ws.recv()
    attached = len(harness.daemon.status().sessions)
    second = await _open(harness)
    await _closes_with(second.ws, captured)
    assert len(harness.daemon.status().sessions) == attached


async def test_a_client_mac_made_with_the_daemon_label_is_refused(
    harness: Harness,
) -> None:
    """Domain separation: the daemon's own proof cannot be sent back as a hello."""
    token = harness.run_dir.read_token()
    opened = await _open(harness)
    reflected = hmac.new(
        token.encode(),
        b"biffd-daemon-v1"
        + opened.challenge.client_nonce.encode()
        + opened.proof.daemon_nonce.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert opened.proof.mac == reflected
    hello = opened.hello(token, mac=reflected)
    assert await _closes_with(opened.ws, Frames.encode(hello)) == 1008


async def test_the_daemons_proof_itself_is_not_a_valid_client_mac(
    harness: Harness,
) -> None:
    opened = await _open(harness)
    hello = opened.hello("x", mac=opened.proof.mac)
    assert await _closes_with(opened.ws, Frames.encode(hello)) == 1008


async def test_the_two_labels_and_the_two_nonce_orders_all_differ() -> None:
    macs = {
        Handshake.daemon_mac("t", "a" * 32, "b" * 32),
        Handshake.daemon_mac("t", "b" * 32, "a" * 32),
        Handshake.client_mac("t", "a" * 32, "b" * 32),
        Handshake.client_mac("t", "b" * 32, "a" * 32),
    }
    assert len(macs) == 4


async def test_each_connection_gets_a_fresh_daemon_nonce(harness: Harness) -> None:
    one = await _open(harness)
    two = await _open(harness)
    await one.ws.close()
    await two.ws.close()
    assert one.proof.daemon_nonce != two.proof.daemon_nonce


async def test_request_before_hello_closes_the_connection(harness: Harness) -> None:
    opened = await _open(harness)
    request = Request(id=1, method="plan", params={"message": "sneaky"})
    assert await _closes_with(opened.ws, Frames.encode(request)) == 1008


async def test_request_before_hello_changes_no_state(harness: Harness) -> None:
    opened = await _open(harness)
    request = Request(id=1, method="plan", params={"message": "sneaky"})
    await _closes_with(opened.ws, Frames.encode(request))
    assert _no_state(harness)


async def test_garbage_before_hello_closes_the_connection(harness: Harness) -> None:
    opened = await _open(harness)
    assert await _closes_with(opened.ws, "{not json") == 1008


async def test_a_second_hello_closes_the_connection(harness: Harness) -> None:
    opened = await _open(harness)
    hello = Frames.encode(opened.hello(harness.run_dir.read_token()))
    assert await _closes_with(opened.ws, hello, hello) == 1008


async def test_hello_without_a_challenge_closes_the_connection(
    harness: Harness,
) -> None:
    opened = await _open(harness)
    await opened.ws.close()
    hello = opened.hello(harness.run_dir.read_token())
    assert await _closes_with(await _unproven(harness), Frames.encode(hello)) == 1008


async def test_hello_without_a_challenge_changes_no_state(harness: Harness) -> None:
    opened = await _open(harness)
    await opened.ws.close()
    hello = opened.hello(harness.run_dir.read_token())
    await _closes_with(await _unproven(harness), Frames.encode(hello))
    assert _no_state(harness)


async def test_a_second_challenge_closes_the_connection(harness: Harness) -> None:
    opened = await _open(harness)
    again = Frames.encode(Challenge.fresh())
    assert await _closes_with(opened.ws, again) == 1008


async def test_a_short_nonce_is_refused(harness: Harness) -> None:
    raw = json.dumps({"type": "challenge", "version": 1, "client_nonce": "ab"})
    assert await _closes_with(await _unproven(harness), raw) == 1008


async def test_unsupported_version_names_both_versions_and_the_fix(
    harness: Harness,
) -> None:
    opened = await _open(harness)
    hello = opened.hello(harness.run_dir.read_token(), version=PROTOCOL_VERSION + 1)
    async with opened.ws:
        await opened.ws.send(Frames.encode(hello))
        reply = Frames.decode(str(await opened.ws.recv()))
    assert isinstance(reply, Reply)
    assert reply.error is not None
    assert reply.error.code is ErrorCode.UNSUPPORTED_VERSION
    message = reply.error.message
    assert str(PROTOCOL_VERSION) in message
    assert str(PROTOCOL_VERSION + 1) in message
    assert "biff daemon restart" in message


async def test_unsupported_version_registers_no_session(harness: Harness) -> None:
    opened = await _open(harness)
    hello = opened.hello(harness.run_dir.read_token(), version=PROTOCOL_VERSION + 1)
    async with opened.ws:
        await opened.ws.send(Frames.encode(hello))
        await opened.ws.recv()
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
                await ws.wait_closed()


def _run_dir_with_token(tmp_path: Path) -> RunDir:
    run_dir = RunDir(tmp_path / "run")
    run_dir.ensure()
    run_dir.token_path.write_text(_TOKEN_MARKER)
    run_dir.token_path.chmod(0o600)
    return run_dir


def _identity() -> ClientIdentity:
    return ClientIdentity(client="cli", session_id="s-kai", cwd="/work/repo")


async def test_an_impostor_with_a_wrong_proof_never_receives_a_hello(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir_with_token(tmp_path)
    forged = Frames.encode(Proof(daemon_nonce="cd" * 16, mac="00" * 32))
    impostor = Impostor(forged)
    async with serve(impostor.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(UnprovenDaemonError, match="did not prove"):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert len(impostor.received) == 1  # only the challenge
    assert all('"hello"' not in frame for frame in impostor.received)
    assert _TOKEN_MARKER not in "".join(impostor.received)


async def test_an_impostor_that_stays_silent_never_receives_a_hello(
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
    assert len(impostor.received) == 1
    assert _TOKEN_MARKER not in "".join(impostor.received)


async def test_an_impostor_that_echoes_the_challenge_gets_no_hello(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir_with_token(tmp_path)
    received: list[str] = []

    async def echo(ws: ServerConnection) -> None:
        async for raw in ws:
            received.append(str(raw))
            challenge = Frames.decode(str(raw))
            assert isinstance(challenge, Challenge)
            nonce = challenge.client_nonce
            await ws.send(Frames.encode(Proof(daemon_nonce=nonce, mac=nonce)))

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(UnprovenDaemonError):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert len(received) == 1


async def test_a_relaying_impostor_learns_no_token_and_no_reusable_credential(
    harness: Harness,
) -> None:
    """The impostor forwards the victim's challenge to the real daemon.

    The real daemon's proof is genuine, so the victim answers with a hello.
    What the impostor holds then is a MAC bound to that connection's two
    nonces: not the token, and refused on any other connection.
    """
    seen: list[str] = []

    async def relay(victim: ServerConnection) -> None:
        upstream = await connect(f"ws://127.0.0.1:{harness.daemon.port}/")
        async for raw in victim:
            seen.append(str(raw))
            await upstream.send(str(raw))
            await victim.send(str(await upstream.recv()))
        await upstream.close()

    # The victim reads the real token file; the impostor sits on the port.
    async with serve(relay, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        victim = await DaemonClient.connect(
            run_dir=harness.run_dir, identity=_identity(), port=port
        )
        await victim.close()
    token = harness.run_dir.read_token()
    assert len(seen) == 2  # challenge, hello
    assert token not in "".join(seen)
    hello = Frames.decode(seen[1])
    assert isinstance(hello, Hello)
    replay = await _open(harness)
    assert await _closes_with(replay.ws, seen[1]) == 1008
    assert hello.mac != Handshake.client_mac(token, "0" * 32, "0" * 32)


async def test_a_hello_for_an_impostors_own_nonce_is_useless_to_the_real_daemon(
    harness: Harness,
) -> None:
    """A MAC bound to a daemon nonce the real daemon never issued is refused."""
    token = harness.run_dir.read_token()
    opened = await _open(harness)
    own_nonce = "ef" * 16
    mac = Handshake.client_mac(token, own_nonce, opened.challenge.client_nonce)
    hello = opened.hello(token, mac=mac)
    assert await _closes_with(opened.ws, Frames.encode(hello)) == 1008
