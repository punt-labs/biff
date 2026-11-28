"""The client never hangs: a dead or misbehaving daemon is an error, not a wait."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

import pytest
from websockets.asyncio.server import ServerConnection, serve

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
    DaemonUnreachableError,
    RequestTimeoutError,
    UnprovenDaemonError,
)
from biff.frame_base import PROTOCOL_VERSION
from biff.frame_client import Hello
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply
from biff.frames import Frames
from biff.log_hint import LOG_HINT
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

_WAIT = 3.0

type Script = Callable[[ServerConnection], Awaitable[None]]


class ScriptedDaemon:
    """A listener that completes the real handshake, then runs a script."""

    def __init__(self, token: str, script: Script, *, reply_to_hello: bool = True):
        self._token = token
        self._script = script
        self._reply_to_hello = reply_to_hello
        self.port = 0

    async def handle(self, ws: ServerConnection) -> None:
        challenge = Frames.decode(str(await ws.recv()))
        assert isinstance(challenge, Challenge)
        nonce = Handshake.fresh_nonce()
        mac = Handshake.daemon_mac(self._token, challenge.client_nonce, nonce)
        await ws.send(Frames.encode(Proof(daemon_nonce=nonce, mac=mac)))
        assert isinstance(Frames.decode(str(await ws.recv())), Hello)
        if self._reply_to_hello:
            await ws.send(Frames.encode(Reply(id=0, result={})))
        await self._script(ws)


def _identity() -> ClientIdentity:
    return ClientIdentity(client="cli", session_id="s-kai", cwd="/work/repo")


async def _client_of(
    run_dir: RunDir,
    script: Script,
    *,
    reply_to_hello: bool = True,
    request_timeout: float = 10.0,
) -> AsyncIterator[DaemonClient]:
    daemon = ScriptedDaemon(
        run_dir.ensure_token(), script, reply_to_hello=reply_to_hello
    )
    async with serve(daemon.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = await DaemonClient.connect(
            run_dir=run_dir,
            identity=_identity(),
            port=port,
            proof_timeout=0.5,
            request_timeout=request_timeout,
        )
        yield client
        await client.close()


@pytest.fixture
def run_dir(tmp_path: Path) -> RunDir:
    return RunDir(tmp_path / "run")


async def _garbage_then_wait(ws: ServerConnection) -> None:
    await ws.send("{this is not a frame")
    await ws.wait_closed()


async def _bytes_garbage_then_wait(ws: ServerConnection) -> None:
    await ws.send(b"\xff\xfe\xfd")
    await ws.wait_closed()


async def _hang_up(ws: ServerConnection) -> None:
    await ws.close()


@pytest.fixture
async def garbled(run_dir: RunDir) -> AsyncIterator[DaemonClient]:
    async for client in _client_of(run_dir, _garbage_then_wait):
        yield client


@pytest.fixture
async def undecodable(run_dir: RunDir) -> AsyncIterator[DaemonClient]:
    async for client in _client_of(run_dir, _bytes_garbage_then_wait):
        yield client


@pytest.fixture
async def hung_up(run_dir: RunDir) -> AsyncIterator[DaemonClient]:
    async for client in _client_of(run_dir, _hang_up):
        yield client


async def test_a_garbled_frame_fails_a_request_instead_of_hanging(
    garbled: DaemonClient,
) -> None:
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(garbled.request("who"), _WAIT)


async def test_an_undecodable_frame_fails_a_request_instead_of_hanging(
    undecodable: DaemonClient,
) -> None:
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(undecodable.request("who"), _WAIT)


async def test_a_request_after_the_loss_fails_at_once(garbled: DaemonClient) -> None:
    await asyncio.sleep(0.2)  # the reader has stopped
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(garbled.request("who"), _WAIT)


async def test_the_client_closes_the_socket_when_its_reader_stops(
    run_dir: RunDir,
) -> None:
    closed = asyncio.Event()

    async def script(ws: ServerConnection) -> None:
        await ws.send("{not a frame")
        await asyncio.wait_for(ws.wait_closed(), _WAIT)
        closed.set()

    async for client in _client_of(run_dir, script):
        await asyncio.wait_for(closed.wait(), _WAIT)
        assert client.pending_events == 0


async def test_next_event_raises_when_the_connection_is_lost(
    hung_up: DaemonClient,
) -> None:
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(hung_up.next_event(), _WAIT)


async def test_next_event_raises_again_on_every_later_call(
    hung_up: DaemonClient,
) -> None:
    for _ in range(2):
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(hung_up.next_event(), _WAIT)


async def test_subscribe_after_the_loss_raises(hung_up: DaemonClient) -> None:
    await asyncio.sleep(0.2)
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(hung_up.subscribe(), _WAIT)


async def test_a_waiting_next_event_is_woken_by_the_loss(run_dir: RunDir) -> None:
    release = asyncio.Event()

    async def script(ws: ServerConnection) -> None:
        await release.wait()
        await ws.close()

    async for client in _client_of(run_dir, script):
        waiting = asyncio.create_task(client.next_event())
        await asyncio.sleep(0.1)
        release.set()
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(waiting, _WAIT)


async def test_events_queued_before_the_loss_are_still_delivered(
    run_dir: RunDir,
) -> None:
    async def script(ws: ServerConnection) -> None:
        event = {"type": "event", "version": 1, "seq": 1, "kind": "unread"}
        await ws.send(json.dumps(event))
        await ws.close()

    async for client in _client_of(run_dir, script):
        assert (await asyncio.wait_for(client.next_event(), _WAIT)).seq == 1
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(client.next_event(), _WAIT)


async def test_a_daemon_that_never_answers_hello_is_a_timeout_not_a_hang(
    run_dir: RunDir,
) -> None:
    async def script(ws: ServerConnection) -> None:
        await ws.wait_closed()

    with pytest.raises(ConnectionLostError):
        async for _ in _client_of(run_dir, script, reply_to_hello=False):
            pass


async def test_a_hello_reply_sent_as_bytes_is_accepted(run_dir: RunDir) -> None:
    async def handle(ws: ServerConnection) -> None:
        challenge = Frames.decode(str(await ws.recv()))
        assert isinstance(challenge, Challenge)
        nonce = Handshake.fresh_nonce()
        mac = Handshake.daemon_mac(run_dir.read_token(), challenge.client_nonce, nonce)
        await ws.send(Frames.encode(Proof(daemon_nonce=nonce, mac=mac)).encode())
        await ws.recv()
        await ws.send(Frames.encode(Reply(id=0, result={})).encode())
        await ws.wait_closed()

    run_dir.ensure_token()
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = await DaemonClient.connect(
            run_dir=run_dir, identity=_identity(), port=port
        )
        await client.close()


async def test_a_daemon_of_another_protocol_version_is_named_not_garbled(
    run_dir: RunDir,
) -> None:
    """The daemon's frame shape may have changed; its version still reads."""

    async def handle(ws: ServerConnection) -> None:
        await ws.recv()
        future = {"type": "proof", "version": PROTOCOL_VERSION + 1, "novel": 1}
        await ws.send(json.dumps(future))
        await ws.wait_closed()

    run_dir.ensure_token()
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(DaemonError) as caught:
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert caught.value.code is ErrorCode.UNSUPPORTED_VERSION
    assert str(PROTOCOL_VERSION) in caught.value.message
    assert str(PROTOCOL_VERSION + 1) in caught.value.message


async def test_an_unsupported_version_reply_to_the_challenge_is_surfaced(
    run_dir: RunDir,
) -> None:
    async def handle(ws: ServerConnection) -> None:
        await ws.recv()
        error = {
            "code": ErrorCode.UNSUPPORTED_VERSION.value,
            "message": "client speaks protocol 1, biffd speaks 2",
        }
        reply = {"type": "reply", "version": 2, "id": 0, "error": error}
        await ws.send(json.dumps(reply))
        await ws.wait_closed()

    run_dir.ensure_token()
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(DaemonError) as caught:
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    assert caught.value.code is ErrorCode.UNSUPPORTED_VERSION


async def test_a_close_names_its_code_and_reason(run_dir: RunDir) -> None:
    async def script(ws: ServerConnection) -> None:
        await ws.close(1008, "unauthorized")

    async for client in _client_of(run_dir, script):
        with pytest.raises(ConnectionLostError, match=r"1008.*unauthorized"):
            await asyncio.wait_for(client.next_event(), _WAIT)


async def test_a_close_during_hello_names_its_code_and_reason(
    run_dir: RunDir,
) -> None:
    async def handle(ws: ServerConnection) -> None:
        challenge = Frames.decode(str(await ws.recv()))
        assert isinstance(challenge, Challenge)
        nonce = Handshake.fresh_nonce()
        mac = Handshake.daemon_mac(run_dir.read_token(), challenge.client_nonce, nonce)
        await ws.send(Frames.encode(Proof(daemon_nonce=nonce, mac=mac)))
        await ws.recv()
        await ws.close(1008, "unauthorized")

    run_dir.ensure_token()
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ConnectionLostError, match=r"1008.*unauthorized"):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)


async def test_a_request_that_is_never_answered_times_out_naming_the_method(
    run_dir: RunDir,
) -> None:
    async def script(ws: ServerConnection) -> None:
        await ws.wait_closed()

    async for client in _client_of(run_dir, script, request_timeout=0.2):
        with pytest.raises(RequestTimeoutError, match="who"):
            await client.request("who")
        assert client._pending == {}


async def test_a_late_reply_after_a_timeout_does_not_break_the_connection(
    run_dir: RunDir,
) -> None:
    async def script(ws: ServerConnection) -> None:
        first = json.loads(str(await ws.recv()))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({"type": "reply", "version": 1, "id": first["id"]}))
        second = json.loads(str(await ws.recv()))
        result = {"ok": True}
        reply = {"type": "reply", "version": 1, "id": second["id"], "result": result}
        await ws.send(json.dumps(reply))
        await ws.wait_closed()

    async for client in _client_of(run_dir, script, request_timeout=0.2):
        with pytest.raises(RequestTimeoutError):
            await client.request("who")
        await asyncio.sleep(0.4)
        assert await client.request("who") == {"ok": True}


async def test_a_reply_with_an_unknown_id_is_logged_with_the_id(
    run_dir: RunDir, caplog: pytest.LogCaptureFixture
) -> None:
    async def script(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"type": "reply", "version": 1, "id": 999}))
        await ws.wait_closed()

    with caplog.at_level(logging.WARNING, logger="biff.client.connection"):
        async for _ in _client_of(run_dir, script):
            await asyncio.sleep(0.2)
    assert any("999" in record.getMessage() for record in caplog.records)


async def test_nothing_listening_is_an_error_that_names_the_port_and_the_repair(
    run_dir: RunDir,
) -> None:
    run_dir.ensure_token()
    pattern = r"127\.0\.0\.1:1.*biff daemon serve"
    with pytest.raises(DaemonUnreachableError, match=pattern):
        await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=1)


async def test_a_corrupt_port_file_is_an_actionable_error(run_dir: RunDir) -> None:
    run_dir.ensure_token()
    run_dir.port_path.write_text("not-a-port\n")
    with pytest.raises(DaemonUnreachableError, match=r"serve\.port"):
        await DaemonClient.connect(run_dir=run_dir, identity=_identity())


async def test_a_listener_that_is_not_a_websocket_is_unproven(
    run_dir: RunDir,
) -> None:
    async def not_websocket(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    run_dir.ensure_token()
    server = await asyncio.start_server(not_websocket, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(UnprovenDaemonError):
            await DaemonClient.connect(run_dir=run_dir, identity=_identity(), port=port)
    finally:
        server.close()
        await server.wait_closed()


async def test_subscribe_waits_for_the_daemons_ok(run_dir: RunDir) -> None:
    async def script(ws: ServerConnection) -> None:
        frame = json.loads(str(await ws.recv()))
        assert frame["type"] == "subscribe"
        await ws.send(json.dumps({"type": "reply", "version": 1, "id": frame["id"]}))
        await ws.wait_closed()

    async for client in _client_of(run_dir, script):
        await asyncio.wait_for(client.subscribe(), _WAIT)


async def test_subscribe_raises_the_daemons_refusal(run_dir: RunDir) -> None:
    async def script(ws: ServerConnection) -> None:
        frame = json.loads(str(await ws.recv()))
        error = {"code": "already_subscribed", "message": "already subscribed"}
        reply = {"type": "reply", "version": 1, "id": frame["id"], "error": error}
        await ws.send(json.dumps(reply))
        await ws.wait_closed()

    async for client in _client_of(run_dir, script):
        with pytest.raises(DaemonError) as caught:
            await asyncio.wait_for(client.subscribe(), _WAIT)
        assert caught.value.code is ErrorCode.ALREADY_SUBSCRIBED


async def test_subscribe_on_a_closed_connection_is_a_lost_connection(
    hung_up: DaemonClient,
) -> None:
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(hung_up.subscribe(), _WAIT)


async def _prove_then_wait(
    token: str, ws: ServerConnection, closed: asyncio.Event
) -> None:
    """Complete the handshake, then record when the client closes the socket."""
    challenge = Frames.decode(str(await ws.recv()))
    assert isinstance(challenge, Challenge)
    nonce = Handshake.fresh_nonce()
    mac = Handshake.daemon_mac(token, challenge.client_nonce, nonce)
    await ws.send(Frames.encode(Proof(daemon_nonce=nonce, mac=mac)))
    await ws.wait_closed()
    closed.set()


async def test_cancelling_connect_while_it_awaits_the_hello_reply_closes_the_socket(
    run_dir: RunDir,
) -> None:
    hello_seen, closed = asyncio.Event(), asyncio.Event()

    async def silent(ws: ServerConnection) -> None:
        hello_seen.set()
        await asyncio.wait_for(ws.wait_closed(), _WAIT)
        closed.set()

    daemon = ScriptedDaemon(run_dir.ensure_token(), silent, reply_to_hello=False)
    async with serve(daemon.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        attempt = asyncio.create_task(
            DaemonClient.connect(
                run_dir=run_dir, identity=_identity(), port=port, proof_timeout=30.0
            )
        )
        await asyncio.wait_for(hello_seen.wait(), _WAIT)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        await asyncio.wait_for(closed.wait(), _WAIT)


async def test_an_identity_the_hello_rejects_closes_the_socket_it_opened(
    run_dir: RunDir,
) -> None:
    closed = asyncio.Event()
    token = run_dir.ensure_token()

    async def handle(ws: ServerConnection) -> None:
        await _prove_then_wait(token, ws, closed)

    bad = ClientIdentity(client="cli", session_id="not a valid id!", cwd="/work/repo")
    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ValueError, match="session_id"):
            await DaemonClient.connect(
                run_dir=run_dir, identity=bad, port=port, proof_timeout=0.5
            )
        await asyncio.wait_for(closed.wait(), _WAIT)


async def test_a_request_timeout_names_the_log_the_daemon_writes(
    run_dir: RunDir,
) -> None:
    async def silent(ws: ServerConnection) -> None:
        await ws.wait_closed()

    async for client in _client_of(run_dir, silent, request_timeout=0.1):
        with pytest.raises(RequestTimeoutError, match=re.escape(LOG_HINT)):
            await client.request("who")


async def _answers_after_a_wait(ws: ServerConnection) -> None:
    request = json.loads(str(await ws.recv()))
    await asyncio.sleep(0.3)
    result = {"confirmed": True}
    reply = {"type": "reply", "version": 1, "id": request["id"], "result": result}
    await ws.send(json.dumps(reply))
    await ws.wait_closed()


async def test_a_request_within_a_longer_deadline_outwaits_the_request_timeout(
    run_dir: RunDir,
) -> None:
    async for client in _client_of(run_dir, _answers_after_a_wait, request_timeout=0.1):
        assert await client.request_within(_WAIT, "channel_check") == {
            "confirmed": True
        }


async def test_a_request_within_forwards_a_seconds_parameter_to_the_method(
    run_dir: RunDir,
) -> None:
    async def echo(ws: ServerConnection) -> None:
        request = json.loads(str(await ws.recv()))
        reply = {
            "type": "reply",
            "version": PROTOCOL_VERSION,
            "id": request["id"],
            "result": request["params"],
        }
        await ws.send(json.dumps(reply))
        await ws.wait_closed()

    async for client in _client_of(run_dir, echo):
        result = await client.request_within(5.0, "channel_check", to="a", seconds=7)
        assert result == {"to": "a", "seconds": 7}


async def test_a_plain_request_gives_up_before_a_slow_answer(
    run_dir: RunDir,
) -> None:
    async for client in _client_of(run_dir, _answers_after_a_wait, request_timeout=0.1):
        with pytest.raises(RequestTimeoutError, match="channel_check"):
            await client.request("channel_check")


async def test_a_request_within_still_times_out_at_its_own_deadline(
    run_dir: RunDir,
) -> None:
    async def silent(ws: ServerConnection) -> None:
        await ws.wait_closed()

    async for client in _client_of(run_dir, silent, request_timeout=5.0):
        with pytest.raises(RequestTimeoutError, match=r"within 0\.1s"):
            await client.request_within(0.1, "channel_check")
